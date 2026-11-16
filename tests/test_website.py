"""The built site. GitHub Pages can't set headers, so the security policy is a meta tag, and the
site lives under /Tuxistant/ there, so nothing may start from the root of the domain."""
import base64
import contextlib
import hashlib
from html import escape as html_escape
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_build():
    spec = importlib.util.spec_from_file_location("site_build", ROOT / "website/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    return build


def releases_shown(html):
    """Each row of the releases list: version, the rest of the row, and its chips as (icon, label)."""
    rows = re.findall(r'<li class="release[^"]*"><span class="rel-version">(.*?)</span>(.*?)'
                      r'<ul class="rel-added">(.*?)</ul></li>', html)
    return [(version, rest, chips_shown(chips)) for version, rest, chips in rows]


def chips_shown(chips):
    return re.findall(r'<li class="feat"><svg class="feat-icon[^"]*" aria-hidden="true" focusable="false">'
                      r'<use href="#feat-([\w-]+)"/></svg><span>([^<]*)</span></li>', chips)


def chips_for(entry, code):
    return [(feature["icon"], html_escape(feature["label"][code])) for feature in entry["features"]]


class BuiltSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build = load_build()
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


    def test_the_main_download_stays_on_the_newest_stable_release(self):
        releases = json.loads((ROOT / "website/src/releases.json").read_text())
        linux = releases["linux"]
        stable = [entry["version"] for entry in releases["history"] if entry["channel"] == "stable"]
        self.assertEqual(linux["channel"], "stable")
        self.assertEqual(linux["version"], stable[0])
        self.assertIn(f"v{linux['version']}.tar.gz", linux["url"])
        for page in self.pages:
            data = json.loads(re.search(r'<script type="application/json" id="strings">(.*?)</script>',
                                        page.read_text(), re.S)[1])
            self.assertEqual(data["releases"]["linux"], linux, page)

    def test_the_preview_is_a_secondary_link_that_says_it_is_untried(self):
        preview = json.loads((ROOT / "website/src/releases.json").read_text())["preview"]
        for page in self.pages:
            if preview is None:     # a stable release newer than any preview: no second link
                self.assertNotIn('class="dl-preview"', page.read_text(), page)
                continue
            strings = self.strings(page)
            line = re.search(r'<p class="dl-preview">(.*?)</p>', page.read_text())[1]
            self.assertIn(f'<a href="{preview["url"]}">', line, page)
            self.assertIn(f'<a href="{preview["page"]}">', line, page)
            self.assertIn(f'</a>{strings["previewSep"]}{strings["previewUntried"]} · ', line, page)
            self.assertIn("Linux", strings["previewTry"], "the preview is a Linux tarball, and says so")
            self.assertIn(preview["version"], line, page)

    def test_the_releases_list_is_newest_first_and_links_each_release(self):
        history = json.loads((ROOT / "website/src/releases.json").read_text())["history"]
        for page in self.pages:
            code = "en" if page.parent == self.dist else page.parent.name
            strings = self.strings(page)
            rows = releases_shown(page.read_text())
            self.assertEqual(len(rows), len(history), page)
            for entry, (version, rest, chips) in zip(history, rows):
                self.assertEqual(version, f'<a href="{entry["page"]}">{entry["version"]}</a>', page)
                self.assertIn(f'<time dir="ltr" datetime="{entry["date"]}">', rest, page)
                self.assertIn(f'>{strings[entry["channel"]]}</span>', rest, page)
                self.assertNotIn("chan-planned", rest, "the real site lists only what is out")
                # Every chip, in the order releases.json gives, which is most impact first.
                self.assertEqual(chips, chips_for(entry, code), (page, entry["version"]))

    def test_mail_search_leads_what_0_3_0_added(self):
        """The owner's call: Mail search comes first in 0.3.0, with a capital."""
        rows = {re.sub(r"<[^>]+>", "", version): chips
                for version, _, chips in releases_shown((self.dist / "index.html").read_text())}
        self.assertEqual(rows["0.3.0"][0], ("mail", "Mail search"))

    def test_each_chip_icon_is_drawn_once_in_one_hidden_sprite(self):
        for page in self.pages:
            html = page.read_text()
            sprites = re.findall(r'<svg class="feat-sprite" aria-hidden="true" focusable="false">(.*?)</svg>',
                                 html, re.S)
            self.assertEqual(len(sprites), 1, page)
            symbols = re.findall(r'<symbol id="feat-([\w-]+)" viewBox="0 0 24 24">', sprites[0])
            self.assertEqual(len(symbols), len(set(symbols)), page)
            used = {icon for _, _, chips in releases_shown(html) for icon, _ in chips}
            self.assertEqual(set(symbols), used, "the sprite holds what the chips use, and only that")
            self.assertNotRegex(sprites[0], r"https?:|//|url\(|<image|<script|style=|href=", page)

    def test_every_chip_label_starts_with_a_capital_where_the_script_has_them(self):
        history = json.loads((ROOT / "website/src/releases.json").read_text())["history"]
        upcoming = json.loads((ROOT / "website/preview/upcoming.json").read_text())["releases"]
        labels = [(entry["version"], code, text) for entry in history + upcoming
                  for feature in entry["features"] for code, text in feature["label"].items()]
        self.assertTrue(labels)
        for version, code, text in labels:
            if text[0].lower() != text[0].upper():
                self.assertTrue(text[0].isupper(), (version, code, text))

    def test_a_slip_in_the_chips_stops_the_build(self):
        languages = self.build.load_languages()
        releases = json.loads((ROOT / "website/src/releases.json").read_text())
        self.build.check_releases(releases, languages)

        def broken(change):
            copy = json.loads(json.dumps(releases))
            change(copy["history"][0])
            return copy
        slips = {
            "an unknown icon": lambda entry: entry["features"][0].update(icon="rocket"),
            "a missing label": lambda entry: entry["features"][1]["label"].pop("zh"),
            "a label in no language of the site": lambda entry: entry["features"][1]["label"].update(fr="Questions"),
            "an empty label": lambda entry: entry["features"][1]["label"].update(es=""),
            "a small letter first": lambda entry: entry["features"][0]["label"].update(pt="habilidades receita"),
            "a sentence, not a chip": lambda entry: entry["features"][0]["label"].update(en="Recipe skills " * 5 + "in YAML"),
            "no features": lambda entry: entry.update(features=[]),
            "a line instead of features": lambda entry: entry.update(added=entry.pop("features")),
        }
        for slip, change in slips.items():
            with self.subTest(slip), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.build.check_releases(broken(change), languages)

    def test_every_language_tags_the_preview_claims_as_english_does(self):
        english = [status for status, _ in self.strings(self.pages[0])["previewCaps"]]
        self.assertTrue(english)
        for page in self.pages:
            caps = self.strings(page)["previewCaps"]
            self.assertEqual([status for status, _ in caps], english, page)
            shown = re.search(r'<ul class="caps caps-row">(.*?)</ul>', page.read_text(), re.S)[1]
            self.assertEqual(len(re.findall(r'<li class="cap">', shown)), len(caps), page)

    def test_the_real_site_holds_no_local_preview(self):
        self.assertFalse((self.dist / "LOCAL-PREVIEW").exists())
        upcoming = json.loads((ROOT / "website/preview/upcoming.json").read_text())
        for page in self.pages:
            html = page.read_text()
            self.assertNotIn("local-preview", html, page)
            self.assertNotIn(upcoming["banner"]["en"], html, page)
            for entry in upcoming["releases"]:
                self.assertNotIn(f">{entry['version']}<", html, page)


class LocalPreview(unittest.TestCase):
    """The preview of planned releases: built beside the real site, and never deployed."""

    @classmethod
    def setUpClass(cls):
        build = load_build()
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        build.DIST = Path(folder.name) / "dist"
        build.DIST_PREVIEW = Path(folder.name) / "dist-preview"
        with contextlib.redirect_stdout(io.StringIO()):
            build.main(["--preview", str(ROOT / "website/preview/upcoming.json")])
        cls.build = build
        cls.pages = sorted(build.DIST_PREVIEW.glob("**/index.html"))
        cls.upcoming = json.loads((ROOT / "website/preview/upcoming.json").read_text())

    def test_it_is_written_beside_the_real_site_not_into_it(self):
        self.assertFalse(self.build.DIST.exists())
        self.assertTrue((self.build.DIST_PREVIEW / "LOCAL-PREVIEW").exists())
        self.assertEqual(len(self.pages), 5)

    def test_every_page_says_it_is_a_local_preview(self):
        for page in self.pages:
            code = "en" if page.parent == self.build.DIST_PREVIEW else page.parent.name
            self.assertIn(f'<p class="local-preview" role="note">{html_escape(self.upcoming["banner"][code])}</p>',
                          page.read_text(), page)

    def test_planned_releases_are_marked_planned_with_their_dates(self):
        planned = self.upcoming["releases"]
        for page in self.pages:
            rows = releases_shown(page.read_text())
            code = "en" if page.parent == self.build.DIST_PREVIEW else page.parent.name
            for entry, (version, rest, chips) in zip(planned, rows):
                self.assertEqual(version, entry["version"], "nothing to link to yet")
                self.assertEqual(chips, chips_for(entry, code), page)
                self.assertIn('class="chan chan-planned"', rest, page)
                self.assertIn(f'<time dir="ltr" datetime="{entry["date"]}">{entry["date"]}</time>', rest, page)
            self.assertEqual(len(rows), len(planned) + len(json.loads(
                (ROOT / "website/src/releases.json").read_text())["history"]), page)

    def test_every_claim_is_still_soon(self):
        for page in self.pages:
            shown = re.search(r'<ul class="caps caps-row">(.*?)</ul>', page.read_text(), re.S)[1]
            self.assertNotIn("tag-now", shown, page)
            code = "en" if page.parent == self.build.DIST_PREVIEW else page.parent.name
            for _, claim in self.upcoming["claims"][code]:
                self.assertIn(html_escape(claim), shown, page)

    def test_a_claim_about_a_desktop_action_says_a_recipe_does_it(self):
        """Only a recipe you add can do one: no built-in skill reaches tuxistant/desktop.py."""
        importers = {file.name for file in (ROOT / "tuxistant").glob("*.py")
                     if re.search(r"(?m)^\s*from \. import .*\bdesktop\b|^\s*from \.desktop ",
                                  file.read_text())}
        self.assertEqual(importers, {"recipes.py"})
        entries = re.findall(r'(?m)^    "(\w+)": Entry\(', (ROOT / "tuxistant/desktop.py").read_text())
        self.assertIn("night_light", entries)
        recipe = {"en": "recipe", "es": "receta", "pt": "receita", "zh": "配方", "ar": "وصف"}
        claims = self.upcoming["claims"]
        for index, (_, claim) in enumerate(claims["en"]):
            if any(entry.replace("_", " ") in claim.casefold() for entry in entries):
                for code, word in recipe.items():
                    with self.subTest(code=code, claim=claim):
                        self.assertIn(word, claims[code][index][1].casefold())

    def test_its_inline_scripts_are_allowed_by_its_csp(self):
        for page in self.pages:
            text = page.read_text()
            policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', text)[1]
            for script in re.findall(r"<script>(.*?)</script>", text, re.S):
                digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                self.assertIn(f"'sha256-{digest}'", policy, page)
            self.assertNotRegex(text, r"""(href|src)=["']/(?!/)""", page)

    def test_deploy_builds_the_real_site_and_refuses_a_preview(self):
        deploy = (ROOT / "website/deploy.sh").read_text()
        self.assertNotIn("--preview", deploy)
        self.assertNotIn("dist-preview", deploy)
        self.assertRegex(deploy, r"python3 build\.py\n")
        self.assertRegex(deploy, r"if \[ -e dist/LOCAL-PREVIEW \]; then .*exit 1")
        self.assertLess(deploy.index("LOCAL-PREVIEW"), deploy.index("git push"))
        self.assertIn("dist-preview/", (ROOT / "website/.gitignore").read_text().split())


if __name__ == "__main__":
    unittest.main()
