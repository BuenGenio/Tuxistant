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
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_build():
    spec = importlib.util.spec_from_file_location("site_build", ROOT / "website/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    return build


def front_pages(dist):
    """One page per language. The skills portal has tests of its own in test_portal.py."""
    return sorted(page for page in dist.glob("**/index.html")
                  if "skills" not in page.relative_to(dist).parts)


def dom_tree(html):
    """The page as tests/site_dom.mjs takes it: ["tag", {attributes}, [children]], text as strings."""
    from html.parser import HTMLParser
    void = {"meta", "link", "br", "img", "input", "source", "hr", "path", "use", "circle"}

    class Builder(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = [["#root", {}, []]]

        def handle_starttag(self, tag, attrs):
            node = [tag, {name: value or "" for name, value in attrs}, []]
            self.stack[-1][2].append(node)
            if tag not in void:
                self.stack.append(node)

        def handle_startendtag(self, tag, attrs):
            self.stack[-1][2].append([tag, {name: value or "" for name, value in attrs}, []])

        def handle_endtag(self, tag):
            if tag not in void:
                while self.stack.pop()[0] != tag:
                    pass

        def handle_data(self, data):
            self.stack[-1][2].append(data)

    builder = Builder()
    builder.feed(html)
    return next(node for node in builder.stack[0][2] if isinstance(node, list))


def releases_shown(html):
    """Each row of the releases list: version, the rest of the row, and its chips as (icon, label)."""
    rows = re.findall(r'<li class="release[^"]*"><span class="rel-version">(.*?)</span>(.*?)'
                      r'<ul class="rel-added" role="list">(.*?)</ul></li>', html)
    return [(version, rest, [(icon, label) for _, icon, label in chips_shown(chips)])
            for version, rest, chips in rows]


def chips_shown(chips):
    """Each chip as (the icon's classes, icon, label)."""
    return re.findall(r'<li class="feat"><svg class="(feat-icon[^"]*)" width="15" height="15" aria-hidden="true" '
                      r'focusable="false"><use href="#feat-([\w-]+)"/></svg><span>([^<]*)</span></li>', chips)


def chips_in(html):
    """Every chip on a page, as chips_shown gives them."""
    return [chip for chips in re.findall(r'<ul class="rel-added" role="list">(.*?)</ul>', html)
            for chip in chips_shown(chips)]


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
        cls.pages = front_pages(build.DIST)
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
        for script in ("site.js", "app.js"):
            self.assertNotIn("'/'", (self.dist / script).read_text(), script)

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

    # Accordion sections, each ending with a call to action.
    ACC_SECTIONS = {    # section: its title, its prompt, and each action in order
        "examples": ("examplesTitle", "ctaExamples", ["download"]),
        "preview": ("previewTitle", "ctaPreview", ["download", "releaseNotes"]),
        "versions": ("versionsTitle", "ctaVersions", ["ctaReleases"]),
    }

    @staticmethod
    def element_tree(html):
        """The page as nested dicts: tag, attributes, element children and text."""
        from html.parser import HTMLParser
        void = {"meta", "link", "br", "img", "input", "source", "hr", "path", "use", "circle"}

        class Builder(HTMLParser):
            def __init__(self):
                super().__init__()
                self.root = {"tag": "#root", "attrs": {}, "children": [], "text": ""}
                self.stack = [self.root]

            def handle_starttag(self, tag, attrs):
                node = {"tag": tag, "attrs": dict(attrs), "children": [], "text": ""}
                self.stack[-1]["children"].append(node)
                if tag not in void:
                    self.stack.append(node)

            def handle_endtag(self, tag):
                if tag not in void:
                    while self.stack.pop()["tag"] != tag:
                        pass

            def handle_data(self, data):
                for node in self.stack:
                    node["text"] += data

        builder = Builder()
        builder.feed(html)
        return builder.root

    @staticmethod
    def find_all(node, test):
        found = [node] if test(node) else []
        for child in node["children"]:
            found += BuiltSite.find_all(child, test)
        return found

    def accordions(self, page):
        tree = self.element_tree(page.read_text())
        by_id = {node["attrs"]["id"]: node for node in self.find_all(tree, lambda n: "id" in n["attrs"])}
        sections = self.find_all(tree, lambda n: n["tag"] == "section"
                                 and "acc" in n["attrs"].get("class", "").split())
        return sections, by_id

    def test_every_section_after_the_ticker_is_an_accordion_that_arrives_open(self):
        for page in self.pages:
            strings = self.strings(page)
            html = page.read_text()
            sections, by_id = self.accordions(page)
            self.assertEqual([s["attrs"]["id"] for s in sections], list(self.ACC_SECTIONS), page)
            self.assertLess(html.index('<div class="ticker">'), html.index('class="section acc wrap"'), page)
            ids = re.findall(r'\sid="([^"]+)"', html)
            self.assertEqual(len(ids), len(set(ids)), "ids are unique")
            for section in sections:
                name = section["attrs"]["id"]
                headings = self.find_all(section, lambda n: n["tag"] == "h2")
                self.assertEqual(len(headings), 1, (page, name))
                heading = headings[0]
                self.assertEqual(section["attrs"]["aria-labelledby"], heading["attrs"]["id"], (page, name))
                # Plain text until app.js makes it the toggle: without script, no button that does nothing.
                self.assertEqual(heading["children"], [], (page, name))
                self.assertEqual(heading["text"], strings[self.ACC_SECTIONS[name][0]], (page, name))
                # The h2 is alone in its box, so fitHeadings() measures the room beside the chevron.
                head = section["children"][0]
                self.assertEqual(head["attrs"].get("class"), "acc-head", (page, name))
                self.assertEqual([child["attrs"].get("class") for child in head["children"]],
                                 ["acc-fit", "acc-chev"], (page, name))
                self.assertEqual(head["children"][0]["children"], [heading], (page, name))
                # Open until script closes it, so a page without script shows everything.
                panel = by_id[f"{name}-panel"]
                self.assertEqual(panel["attrs"].get("class"), "acc-panel", (page, name))
                self.assertNotIn("hidden", panel["attrs"], (page, name))
                self.assertEqual(section["children"], [head, panel], "everything but the heading folds")
            self.assertNotIn("is-closed", html, "only app.js closes a section")
            self.assertNotIn("acc-toggle", html, "only app.js makes the toggle")
            self.assertNotIn("aria-expanded", html, "only app.js makes the toggle")

    def test_every_section_ends_with_a_call_to_action_in_its_language(self):
        releases = json.loads((ROOT / "website/src/releases.json").read_text())
        notes = self.build.release_in(releases, releases["linux"]["version"])["page"]
        hrefs = {"download": releases["linux"]["url"], "releaseNotes": notes,
                 "ctaReleases": "https://github.com/BuenGenio/Tuxistant/releases"}
        for page in self.pages:
            strings = self.strings(page)
            # Without script the download is the Linux file, so it says Linux, as the hero's does.
            labels = dict(strings, download=self.build.fill(strings["dlFor"], os="Linux"))
            sections, _ = self.accordions(page)
            for section in sections:
                name = section["attrs"]["id"]
                _, prompt, actions = self.ACC_SECTIONS[name]
                node = section      # follow the last element down: it must reach the call to action
                while "acc-cta" not in node["attrs"].get("class", "").split():
                    self.assertTrue(node["children"], (page, name, "has no call to action at its end"))
                    node = node["children"][-1]
                self.assertEqual(node["children"][0]["tag"], "p", (page, name))
                self.assertEqual(node["children"][0]["text"], strings[prompt], (page, name))
                links = self.find_all(node, lambda n: n["tag"] == "a")
                self.assertEqual([link["text"] for link in links], [labels[key] for key in actions], (page, name))
                self.assertEqual([link["attrs"]["href"] for link in links], [hrefs[key] for key in actions], (page, name))
                self.assertIn("btn-cta", links[0]["attrs"]["class"].split(), "the first action is a button")
                self.assertFalse(any("hidden" in link["attrs"] for link in links), (page, name))
        for key in ("ctaExamples", "ctaPreview", "ctaVersions", "ctaReleases", "ctaSoon", "ctaSoonAlso"):
            labels = [self.strings(page)[key] for page in self.pages]
            self.assertTrue(all(label.strip() for label in labels), key)
            self.assertEqual(len(set(labels)), 5, f"{key}: each language has its own words")
        for page in self.pages:
            for key in ("ctaSoon", "ctaSoonAlso"):
                self.assertEqual(re.findall(r"\{(\w+)\}", self.strings(page)[key]), ["os"], (page, key))

    def test_the_donate_card_is_the_one_donation_ask_besides_the_header(self):
        """An open-source page with a Donate button in every view reads as pushy, so no section's
        call to action repeats the card just below it."""
        for page in self.pages:
            tree = self.element_tree(page.read_text())
            asks = self.find_all(tree, lambda n: n["tag"] == "a" and n["attrs"].get("href") == self.build.DONATE)
            self.assertEqual(len(asks), 2, page)
            header = self.find_all(tree, lambda n: n["attrs"].get("id") == "site-header")[0]
            card = self.find_all(tree, lambda n: "support" in n["attrs"].get("class", "").split())[0]
            self.assertIn(asks[0], self.find_all(header, lambda n: n["tag"] == "a"), page)
            self.assertIn(asks[1], self.find_all(card, lambda n: n["tag"] == "a"), page)
            self.assertNotIn('data-cta="donate"', page.read_text(), page)

    def test_the_examples_ask_about_the_system_on_show(self):
        """A Mac visitor opens on the macOS tab, where nothing is out yet: the examples must not then
        ask whether they want it on their desktop. test_app_js_fits_each_visitor runs the code."""
        app = (self.dist / "app.js").read_text()
        select = app[app.index("function selectTab("):app.index("function examplesAsk(")]
        self.assertIn("examplesAsk(id);", select, "the ask follows every change of tab")
        ask = app[app.index("function examplesAsk("):]
        ask = ask[:ask.index("\n  }\n") + 4]
        self.assertIn("document.querySelector('#examples .acc-cta p')", ask, "the examples' own prompt")
        for page in self.pages:
            data = json.loads(re.search(r'<script type="application/json" id="strings">(.*?)</script>',
                                        page.read_text(), re.S)[1])
            strings = self.strings(page)
            self.assertEqual((data["ctaExamples"], data["ctaSoon"], data["ctaSoonAlso"]),
                             (strings["ctaExamples"], strings["ctaSoon"], strings["ctaSoonAlso"]), page)
            # Two sentences, so a narrow screen can break between them.
            self.assertTrue(strings["ctaSoon"].rstrip().endswith((".", "。")), page)
        css = (self.dist / "style.css").read_text()
        self.assertIn(".acc-cta p span { display: inline-block; }", css, "a line breaks between sentences")

    def test_a_section_download_names_the_system_it_downloads_for(self):
        """Phones, macOS and Windows have no release of their own and get Linux's, as the hero does,
        so the button says Linux; only a system with its own release gets its own file."""
        app = (self.dist / "app.js").read_text()
        block = app[app.index("const ctaOS = "):]
        block = block[:block.index("\n  }\n")]
        self.assertIn("const ctaOS = S.releases[os]?.url ? os : 'linux';", block)
        self.assertIn("document.querySelectorAll('[data-cta=\"download\"]')", block)
        self.assertIn("link.href = S.releases[ctaOS].url;", block)
        self.assertIn("link.textContent = fill(S.dlFor, { os: OS_NAMES[ctaOS] });", block)
        self.assertRegex(app, r"if \(/android\|iphone\|ipad\|ipod/\.test\(ua\)\) return null;",
                         "a phone is no system: it falls through to Linux")
        # What app.js relabels is exactly the sections' download buttons.
        linux = json.loads((ROOT / "website/src/releases.json").read_text())["linux"]["url"]
        for page in self.pages:
            tree = self.element_tree(page.read_text())
            marked = self.find_all(tree, lambda n: n["attrs"].get("data-cta") == "download")
            ctas = self.find_all(tree, lambda n: "acc-cta" in n["attrs"].get("class", "").split())
            buttons = [link for cta in ctas for link in self.find_all(cta, lambda n: n["tag"] == "a")
                       if link["attrs"].get("href") == linux]
            self.assertEqual(marked, buttons, page)
            self.assertEqual(len(marked), 2, page)

    AGENTS = {      # a visitor: user agent, platform, and the system detectOS() sees (None: a phone)
        "phone": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1", "iPhone", None),
        "mac": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/18.1 Safari/605.1.15", "MacIntel", "macos"),
        "windows": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36", "Win32", "windows"),
        "linux": ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
                  "Linux x86_64", "linux"),
    }

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_app_js_fits_each_visitor(self):
        """Runs app.js on every page as a phone, a Mac, a Windows PC and a Linux desktop would, then
        opens each tab: what the hero and the sections offer, and what the examples ask."""
        releases = json.loads((ROOT / "website/src/releases.json").read_text())
        names = dict(self.build.OS_ORDER)
        tabs = [None] + [f"#tab-{os_id}" for os_id in names]
        probes = ["#download", "#examples .acc-cta p", "#examples .acc-cta a", "#preview .acc-cta p",
                  "#preview .acc-cta a", "[data-cta=\"download\"]", ".acc-toggle", "[role=\"tab\"][aria-selected=\"true\"]"]
        pages = {str(page): dom_tree(page.read_text()) for page in self.pages}
        cases = [{"page": str(page), "userAgent": agent, "platform": platform, "steps": tabs, "probes": probes}
                 for page in self.pages for agent, platform, _ in self.AGENTS.values()]
        with tempfile.TemporaryDirectory() as folder:
            given, report = Path(folder) / "input.json", Path(folder) / "report.json"
            given.write_text(json.dumps({"app": str(self.dist / "app.js"), "pages": pages, "cases": cases}))
            subprocess.run(["node", str(ROOT / "tests/site_dom.mjs"), str(given), str(report)],
                           capture_output=True, timeout=300, check=True)
            reports = iter(json.loads(report.read_text()))
        for page in self.pages:
            strings = self.strings(page)
            for visitor, (_, _, own) in self.AGENTS.items():
                where = (page.relative_to(self.dist), visitor)
                steps = next(reports)
                self.assertNotIn("error", steps, where)
                # The section downloads are the hero's file: the visitor's own when there is one.
                cta_os = own if own and releases[own]["url"] else "linux"
                label = self.build.fill(strings["dlFor"], os=names[cta_os])
                for step, tab in zip(steps, tabs):
                    on_show = tab[len("#tab-"):] if tab else own or "linux"
                    where = (page.relative_to(self.dist), visitor, on_show)
                    self.assertEqual([t["attrs"]["data-os"] for t in step[probes[7]]], [on_show], where)
                    downloads = step['[data-cta="download"]']
                    self.assertEqual([(d["text"], d["attrs"]["href"]) for d in downloads],
                                     [(label, releases[cta_os]["url"])] * 2, where)
                    self.assertEqual(step["#download"][0]["attrs"].get("href"), releases[own or "linux"]["url"], where)
                    # The examples end on one action, the download, and ask about the tab on show.
                    self.assertEqual(step["#examples .acc-cta a"], downloads[:1], where)
                    prompt = step["#examples .acc-cta p"][0]
                    if releases[on_show]["url"]:
                        self.assertEqual(prompt["text"], strings["ctaExamples"], where)
                        continue
                    soon = self.build.fill(strings["ctaSoon"], os=names[on_show])
                    also = self.build.fill(strings["ctaSoonAlso"], os=names[cta_os])
                    gap = [] if soon.endswith("。") else [["#text", " "]]
                    self.assertEqual(prompt["parts"], [["span", soon], *gap, ["span", also]], where)
                # New since 0.2.0 keeps its own prompt and both actions.
                last = steps[-1]
                self.assertEqual([p["text"] for p in last["#preview .acc-cta p"]], [strings["ctaPreview"]], where)
                self.assertEqual([a["text"] for a in last["#preview .acc-cta a"]], [label, strings["releaseNotes"]], where)
                # The first section stays open and the rest fold.
                self.assertEqual([t["attrs"]["aria-expanded"] for t in last[".acc-toggle"]],
                                 ["true", "false", "false"], where)

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_a_link_into_a_folded_section_opens_it_under_the_header(self):
        """The hero's "All versions", or an address that names a section or something in one, opens
        that section, which arrives folded, and keeps the header showing for the jump."""
        probes = [".acc-toggle", "#site-header", "#scrolled"]
        cases = [(None, ['.hero a[href="#versions"]'], "versions", None),
                 ("#versions", [], "versions", "versions"),
                 ("#releases-title", [], "versions", "releases-title"),
                 ("#preview", [], "preview", "preview")]
        pages = {str(page): dom_tree(page.read_text()) for page in self.pages}
        agent, platform, _ = self.AGENTS["phone"]
        runs = [{"page": str(page), "userAgent": agent, "platform": platform, "hash": hash_ or "",
                 "steps": [None, *clicks], "probes": probes}
                for page in self.pages for hash_, clicks, _, _ in cases]
        with tempfile.TemporaryDirectory() as folder:
            given, report = Path(folder) / "input.json", Path(folder) / "report.json"
            given.write_text(json.dumps({"app": str(self.dist / "app.js"), "pages": pages, "cases": runs}))
            subprocess.run(["node", str(ROOT / "tests/site_dom.mjs"), str(given), str(report)],
                           capture_output=True, timeout=300, check=True)
            reports = iter(json.loads(report.read_text()))
        for page in self.pages:
            for hash_, _, opened, scrolled in cases:
                where = (page.relative_to(self.dist), hash_)
                steps = next(reports)
                self.assertNotIn("error", steps, where)
                last = steps[-1]
                expanded = {t["attrs"]["id"]: t["attrs"]["aria-expanded"] for t in last[".acc-toggle"]}
                # The first section stays open, and the one the link names opens with it.
                self.assertEqual(expanded, {f"{name}-toggle": str(name in ("examples", opened)).lower()
                                            for name in self.ACC_SECTIONS}, where)
                self.assertNotIn("is-hidden", last["#site-header"][0]["attrs"]["class"], where)
                # An address opens the section and then scrolls to what it names: the browser had
                # nothing open to scroll to. A click opens it first, and the browser follows the link.
                self.assertEqual([s["attrs"]["id"] for s in last["#scrolled"]], [scrolled] if scrolled else [], where)

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_folding_a_section_moves_focus_out_and_leaves_its_text_findable(self):
        """Closing a section with the focus inside it moves the focus to its toggle, as Safari leaves
        it on what had it. A closed panel is hidden until found where the browser knows that, so
        find in page and a link to a passage of text still reach it, and plainly hidden elsewhere."""
        link = "#examples .acc-cta a"
        probes = ["#active", ".acc-panel", "#examples"]
        pages = {str(page): dom_tree(page.read_text()) for page in self.pages}
        agent, platform, _ = self.AGENTS["linux"]
        runs = [{"page": str(page), "userAgent": agent, "platform": platform, "beforematch": found,
                 "steps": [None, {"focus": link}, "#examples-toggle"], "probes": probes}
                for page in self.pages for found in (True, False)]
        with tempfile.TemporaryDirectory() as folder:
            given, report = Path(folder) / "input.json", Path(folder) / "report.json"
            given.write_text(json.dumps({"app": str(self.dist / "app.js"), "pages": pages, "cases": runs}))
            subprocess.run(["node", str(ROOT / "tests/site_dom.mjs"), str(given), str(report)],
                           capture_output=True, timeout=300, check=True)
            reports = iter(json.loads(report.read_text()))
        for page in self.pages:
            for found in (True, False):
                where = (page.relative_to(self.dist), "until-found" if found else "hidden")
                arrived, focused, folded = next(reports)
                hidden = {panel["attrs"]["id"]: panel["attrs"].get("hidden") for panel in arrived[".acc-panel"]}
                self.assertEqual(hidden, {"examples-panel": None, "preview-panel": "until-found" if found else "",
                                          "versions-panel": "until-found" if found else ""}, where)
                self.assertEqual([a["attrs"].get("data-cta") for a in focused["#active"]], ["download"], where)
                self.assertIn("is-closed", folded["#examples"][0]["attrs"]["class"], where)
                self.assertEqual([a["attrs"].get("id") for a in folded["#active"]], ["examples-toggle"], where)

    def test_the_releases_chips_fold_with_the_versions_and_come_before_its_call_to_action(self):
        for page in self.pages:
            html = page.read_text()
            sections, by_id = self.accordions(page)
            versions = next(s for s in sections if s["attrs"]["id"] == "versions")
            inner = by_id["versions-panel"]["children"][0]
            classes = [child["attrs"].get("class", "") for child in inner["children"]]
            self.assertIn("releases", classes, page)
            self.assertEqual(classes[-1], "acc-cta", page)
            self.assertLess(classes.index("releases"), classes.index("acc-cta"), page)
            # The icons the chips draw are in the same panel, so they fold and show with them.
            sprite = self.find_all(versions, lambda n: n["tag"] == "symbol" and n["attrs"].get("id", "").startswith("feat-"))
            self.assertTrue(sprite, page)
            self.assertEqual(len(re.findall(r'<symbol id="feat-', html)), len(sprite), page)
            # The hero, with its theme behind the title, comes before the ticker and the sections.
            self.assertLess(html.index('id="hero"'), html.index('<div class="ticker">'), page)

    def test_with_no_linux_download_a_section_offers_every_release_instead(self):
        releases = json.loads((ROOT / "website/src/releases.json").read_text())
        strings = self.strings(self.pages[0])
        self.assertIn('data-cta="download"', self.build.cta_download(strings, releases))
        releases["linux"]["url"] = None     # "coming soon": nothing to download
        offered = self.build.cta_download(strings, releases)
        self.assertNotIn('data-cta="download"', offered)
        self.assertNotIn("#download", offered)
        self.assertEqual(offered, self.build.cta_releases(strings, releases))

    def test_every_in_page_link_lands_on_something(self):
        for page in self.pages:
            html = page.read_text()
            for target in re.findall(r'href="#([^"]*)"', html):
                self.assertRegex(html, rf'\sid="{re.escape(target)}"', (page, target))
            self.assertIn('href="#versions"', html, "the hero links the versions section")

    def test_the_accordion_folds_only_with_script_and_respects_motion_and_print(self):
        css = (self.dist / "style.css").read_text()
        app = (self.dist / "site.js").read_text()     # every page's, as the portal folds too
        block = css[css.index("Accordion sections and their calls to action: start"):
                    css.index("Accordion sections and their calls to action: end")]
        self.assertRegex(block, r"\.acc-chev \{[^}]*display: none;", "no chevron without script")
        self.assertIn(".has-acc .acc-chev { display: block; }", block)
        self.assertIn(".acc.is-closed .acc-panel { grid-template-rows: 0fr; }", block)
        self.assertRegex(block, r"@media \(prefers-reduced-motion: reduce\) \{\s*\.acc-panel, \.acc-chev \{ transition: none; \}")
        self.assertRegex(block, r"@media print \{\s*\.acc \.acc-panel, \.acc \.acc-panel\[hidden\] "
                                r"\{ display: block; content-visibility: visible; \}")
        self.assertIn("classList.add('has-acc')", app)
        self.assertIn("animate = !reducedMotion", app, "the fold is instant for reduced motion")
        # The toggle: made from the heading's text, inside the h2, open, and naming its panel.
        make = app[app.index("for (const heading of document.querySelectorAll('.acc-head h2'))"):]
        make = make[:make.index("\n  }\n")]
        for line in ("document.createElement('button')", "toggle.type = 'button';", "toggle.className = 'acc-toggle';",
                     "toggle.setAttribute('aria-controls', section.querySelector('.acc-panel').id);",
                     "toggle.setAttribute('aria-expanded', 'true');", "toggle.append(...heading.childNodes);",
                     "heading.append(toggle);"):
            self.assertIn(line, make)
        self.assertLess(app.index("heading.append(toggle);"), app.index("const accordions = "))
        # Where hidden=until-found is unknown, a closed panel must leave layout and the tab order.
        self.assertIn('.acc-panel[hidden]:not([hidden="until-found"]) { display: none; }', block)
        # The toggle's hit area is the whole row, so a tap on the chevron folds too.
        self.assertRegex(block, r'\.acc-toggle::after \{ content: ""; position: absolute; inset: 0 -8px;')
        self.assertRegex(block, r"\.acc-head \{ position: relative;")
        # A touch screen keeps :hover after a tap, so the chevron's highlight is for pointers only.
        self.assertRegex(block, r"@media \(hover: hover\) \{\s*\.acc-head:hover \.acc-chev \{")
        self.assertNotRegex(block.replace("@media (hover: hover) {\n  .acc-head:hover", ""), r"\.acc-head:hover")
        # The Donate card's margin is the only gap after the last section: margins never stack.
        self.assertIn(".acc:has(+ .wrap > .support) .acc-cta { margin-block-end: 0; }", block)
        # A link is as tall as the button beside it.
        self.assertRegex(block, r"\.acc-more \{[^}]*min-height: 44px;")
        # Following an in-page link scrolls before hashchange fires, so the header's grace starts on
        # the click, ahead of the scroll.
        self.assertRegex(app, r"document\.addEventListener\('click', event => \{\s*"
                              r"if \(event\.target\.closest\?\.\('a\[href\^=\"#\"\]'\)\) "
                              r"(graceUntil = performance\.now\(\) \+ 900|navigated\(\))")
        for opened_by in ("addEventListener('hashchange', () => reveal(location.hash)",
                          "reveal(location.hash)?.scrollIntoView();", "reveal(link.hash)", "'beforematch'"):
            self.assertIn(opened_by, app, "a link, a hash or find in page opens its section")


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

    def test_icons_with_writing_are_mirrored_right_to_left(self):
        mirrored = self.build.FEATURE_MIRRORED
        self.assertTrue(mirrored <= set(self.build.FEATURE_ICONS))
        for page in self.pages:
            chips = chips_in(page.read_text())
            self.assertTrue(chips, page)
            for classes, icon, _ in chips:
                self.assertEqual("is-mirrored" in classes.split(), icon in mirrored, (page, icon))
        self.assertTrue(any(icon in mirrored for _, icon, _ in chips), "the test sees a mirrored icon")
        css = (self.dist / "style.css").read_text()
        self.assertRegex(css, r'\[dir="rtl"\] \.feat-icon\.is-mirrored \{ transform: scaleX\(-1\); \}')

    def test_a_chip_label_is_escaped(self):
        entry = {"features": [{"icon": "mail", "label": {"en": 'Q&A <b>"x"'}}]}
        shown = self.build.features_html("en", entry)
        self.assertIn("<span>Q&amp;A &lt;b&gt;&quot;x&quot;</span>", shown)
        self.assertNotIn("<b>", shown)

    def test_chip_icons_take_their_size_without_the_stylesheet(self):
        for page in self.pages:
            html = page.read_text()
            self.assertIn('<svg class="feat-sprite" width="0" height="0" aria-hidden="true"', html, page)
            self.assertEqual(len(re.findall(r'<svg class="feat-icon', html)), len(chips_in(html)), page)

    def test_mail_search_leads_what_0_3_0_added(self):
        """The owner's call: Mail search comes first in 0.3.0, with a capital."""
        rows = {re.sub(r"<[^>]+>", "", version): chips
                for version, _, chips in releases_shown((self.dist / "index.html").read_text())}
        self.assertEqual(rows["0.3.0"][0], ("mail", "Mail search"))

    def test_each_chip_icon_is_drawn_once_in_one_hidden_sprite(self):
        for page in self.pages:
            html = page.read_text()
            sprites = re.findall(r'<svg class="feat-sprite" width="0" height="0" aria-hidden="true" focusable="false">(.*?)</svg>',
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
            "one character past a phone's line": lambda entry: entry["features"][0]["label"].update(en="A" * 41),
            "a label with a space at the end": lambda entry: entry["features"][1]["label"].update(en="Follow-up questions "),
            "a number as a label": lambda entry: entry["features"][1]["label"].update(en=5),
            "a number for every label": lambda entry: entry["features"][1].update(label=5),
            "a list as the icon": lambda entry: entry["features"][0].update(icon=["mail"]),
            "a key besides icon and label": lambda entry: entry["features"][0].update(note="x"),
            "a line where a feature belongs": lambda entry: entry["features"].append("Mail search"),
            "features as one dict": lambda entry: entry.update(features=entry["features"][0]),
            "no features": lambda entry: entry.update(features=[]),
            "a line instead of features": lambda entry: entry.update(added=entry.pop("features")),
        }
        for slip, change in slips.items():
            with self.subTest(slip), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.build.check_releases(broken(change), languages)
        longest = self.build.FEATURE_LABEL_LENGTH[1]
        self.assertEqual(longest, 40, "about one line in a chip at 360 px")
        at_the_limit = broken(lambda entry: entry["features"][0]["label"].update(en="A" * longest))
        self.build.check_releases(at_the_limit, languages)

    def test_a_small_letter_first_is_named_as_the_slip(self):
        languages = self.build.load_languages()
        releases = json.loads((ROOT / "website/src/releases.json").read_text())
        releases["history"][0]["features"][0]["label"]["pt"] = "habilidades de receita"
        with self.assertRaises(SystemExit) as stop:
            self.build.check_releases(releases, languages)
        self.assertIn("must start with a capital letter", str(stop.exception))

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

    def test_the_real_site_holds_no_skills_portal(self):
        """The owner's call (2026-11-14): the portal stays out of deploys until the registry repo
        exists, so the plain build writes no page of it, nor its script, nor the sample."""
        self.assertFalse((self.dist / "skills").exists())
        for portal in ("skills.js", "skills.css"):
            self.assertFalse((self.dist / portal).exists(), portal)
        self.assertFalse((self.dist / "LOCAL-PORTAL").exists())
        sample = ROOT / "website/src/registry-sample"
        self.assertFalse({file.name for file in sample.rglob("*")}
                         & {file.name for file in self.dist.rglob("*")})
        for page in self.pages:
            html = page.read_text()
            for portal in ("skills/", "nav-link", "lang-pick", "example-ada"):
                self.assertNotIn(portal, html, page)

    def test_the_plain_build_reads_no_registry(self):
        build = load_build()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        build.DIST = Path(folder.name) / "dist"
        refuse = AssertionError("the plain build loaded portal.py")
        with mock.patch.object(build, "_portal", side_effect=refuse), \
                contextlib.redirect_stdout(io.StringIO()):
            build.main()
        self.assertEqual(front_pages(build.DIST), sorted(build.DIST.glob("**/index.html")))


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
        cls.pages = front_pages(build.DIST_PREVIEW)
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
        for local in ("--preview", "dist-preview", "--portal", "dist-portal"):
            self.assertNotIn(local, deploy)
        self.assertRegex(deploy, r"python3 build\.py\n")
        self.assertRegex(deploy, r"if \[ -e dist/LOCAL-PREVIEW \]; then .*exit 1")
        self.assertLess(deploy.index("LOCAL-PREVIEW"), deploy.index("git push"))
        self.assertNotIn("Co-Authored-By", deploy, "the published commit is the owner's alone")
        self.assertIn("dist-preview/", (ROOT / "website/.gitignore").read_text().split())


# deploy.sh's stand-ins: a build.py that writes dist/ with what the test asks, and a git that only
# writes down what it was asked. Nothing is built, committed or pushed for real.
STAND_IN_BUILD = """import os, sys
from pathlib import Path
with open(os.environ["CALLS"], "a") as calls:
    calls.write(" ".join(["build.py", *sys.argv[1:]]) + "\\n")
dist = Path("dist")
dist.mkdir()
(dist / "index.html").write_text("<!doctype html>")
for name in os.environ["LEAVES"].split():
    (dist / name).parent.mkdir(parents=True, exist_ok=True)
    (dist / name).write_text("")
"""
STAND_IN_GIT = """#!/bin/sh
echo "git $*" >> "$CALLS"
case "$1" in remote) echo https://example.org/site.git ;; rev-parse) echo abc1234 ;; esac
"""


class Deploy(unittest.TestCase):
    """deploy.sh itself, run from a copy beside stand-ins for build.py and git, in a folder that
    is no git repo, so it can't reach a real one even if the stand-in were missed."""

    def deploy(self, *leaves):
        folder = Path(tempfile.mkdtemp(prefix="tux-deploy-"))
        self.addCleanup(shutil.rmtree, folder, True)
        site, tools = folder / "website", folder / "bin"
        site.mkdir()
        tools.mkdir()
        shutil.copy(ROOT / "website/deploy.sh", site)
        (site / "build.py").write_text(STAND_IN_BUILD)
        (tools / "git").write_text(STAND_IN_GIT)
        (tools / "git").chmod(0o755)
        calls = folder / "calls"
        calls.touch()
        env = {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(folder), "CALLS": str(calls),
               "LEAVES": " ".join(leaves), "GIT_CEILING_DIRECTORIES": str(folder.parent)}
        done = subprocess.run(["/bin/sh", str(site / "deploy.sh")], capture_output=True, text=True,
                              timeout=60, env=env)
        return done, calls.read_text().splitlines()

    def test_the_real_site_is_built_with_no_options_and_pushed(self):
        done, calls = self.deploy()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("build.py", calls)
        self.assertIn("git push -q -f https://example.org/site.git gh-pages", calls)

    def test_a_dist_holding_the_portal_or_a_preview_is_never_pushed(self):
        for left, why in (("LOCAL-PORTAL", "skills portal"), ("skills/index.html", "skills portal"),
                          ("LOCAL-PREVIEW", "local preview")):
            with self.subTest(left):
                done, calls = self.deploy(left)
                self.assertEqual(done.returncode, 1)
                self.assertIn(why, done.stderr)
                pushed = [call for call in calls if call.startswith(("git init", "git push"))]
                self.assertEqual(pushed, [])


class HeroAndHeader(unittest.TestCase):
    """The themes fill the hero behind the title, and the header blurs a layer inside it, never
    itself: it moves with transform, and a moving element has nothing behind it to blur."""

    @classmethod
    def setUpClass(cls):
        build = load_build()
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        build.DIST = Path(folder.name) / "dist"
        with contextlib.redirect_stdout(io.StringIO()):
            build.main()
        cls.build = build
        cls.pages = front_pages(build.DIST)
        cls.css = (build.DIST / "style.css").read_text()
        # What a front page runs: site.js, every page's, then app.js.
        cls.js = (build.DIST / "site.js").read_text() + (build.DIST / "app.js").read_text()
        plain = re.sub(r"/\*.*?\*/", "", cls.css, flags=re.S)
        # Innermost rules only: an @media or @supports wrapper drops out, its rules stay.
        cls.rules = [(selectors.strip(), body) for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", plain)]

    def rule(self, selector):
        """The declarations of every rule whose selector list holds this selector."""
        bodies = [body for selectors, body in self.rules
                  if selector in [one.strip() for one in selectors.split(",")]]
        self.assertTrue(bodies, selector)
        return " ".join(bodies)

    def hero(self, page):
        match = re.search(r'<section class="hero" id="hero" data-theme="(\w+)">(.*?)</section>',
                          page.read_text(), re.S)
        self.assertTrue(match, page)
        return match[1], match[2]

    def test_the_theme_lies_behind_the_title_edge_to_edge(self):
        for page in self.pages:
            theme, hero = self.hero(page)
            self.assertEqual(theme, self.build.THEME_ORDER[0], page)
            self.assertIn('<canvas id="tux" aria-hidden="true"></canvas>', hero, page)
            self.assertIn('<div class="fallback" aria-hidden="true"></div>', hero, page)
            self.assertLess(hero.index('<canvas id="tux"'), hero.index("<h1>Tuxistant</h1>"), page)
            self.assertLess(hero.index("<h1>"), hero.index('id="download"'), page)
        canvas = self.rule("#tux")
        for declaration in ("position: absolute", "inset: 0", "width: 100%", "height: 100%",
                            "pointer-events: none"):     # never takes a tap, a click or a scroll
            self.assertIn(declaration, canvas)
        self.assertRegex(canvas, r"z-index: -\d")
        self.assertIn("isolation: isolate", self.rule(".hero"), "below zero, yet still inside the hero")
        self.assertIn("margin-block-start: calc(-1 * var(--header-h))", self.rule("main > .hero:first-child"),
                      "on up under the header's glass")

    def test_the_theme_picker_stays_in_the_hero(self):
        first = self.build.THEME_ORDER[0]
        for page in self.pages:
            code = "en" if page.parent == self.build.DIST else page.parent.name
            strings = json.loads((ROOT / "website/src/i18n" / f"{code}.json").read_text())
            self.assertEqual(set(strings["themes"]), set(self.build.THEME_ORDER), page)
            _, hero = self.hero(page)
            self.assertIn(f'<div class="pick"><span id="theme-name">{html_escape(strings["themes"][first])}'
                          '</span><span class="dots" id="dots"></span></div>', hero, page)

    def test_every_theme_has_a_still_drawing_for_browsers_without_webgl(self):
        self.assertIn("display: none", self.rule(".has-gl .fallback"))
        for theme in self.build.THEME_ORDER:
            self.assertIn("background:", self.rule(f'.hero[data-theme="{theme}"] .fallback::before'), theme)
        # It goes where the shader would: the script places it.
        self.assertRegex(self.js, r"function standIn\(\) \{[^}]*place\(themes\[current\]\)[^}]*"
                                  r"setProperty\(`--actor-\$\{key\}`")
        for key in ("left", "top", "width", "height"):
            self.assertIn(f"var(--actor-{key}", self.rule(".fallback::before"), key)

    def test_the_still_drawing_never_flashes_before_the_script_decides(self):
        """The page head marks the page as scripted before first paint. Until the script knows
        whether WebGL runs, the drawing is hidden; without a script it stays."""
        self.assertIn("document.documentElement.classList.add('js')", self.build.MODE_SCRIPT)
        for page in self.pages:
            head = page.read_text().split("</head>")[0]
            self.assertIn(f"<script>{self.build.MODE_SCRIPT}</script>", head, page)
        self.assertIn("visibility: hidden", self.rule(".js:not(.no-gl) .fallback"))
        self.assertIn("root.classList.add(gl ? 'has-gl' : 'no-gl')", self.js)
        # Without a script there is nothing to pick with, so no picker either.
        self.assertIn("display: none", self.rule(":root:not(.js) .pick"))

    def test_a_lost_webgl_context_brings_the_still_drawing_back(self):
        lost = re.search(r"addEventListener\('webglcontextlost', event => \{(.*?)\n    \}\);", self.js, re.S)
        self.assertTrue(lost)
        for step in ("event.preventDefault()", "root.classList.replace('has-gl', 'no-gl')", "standIn()"):
            self.assertIn(step, lost[1])
        restored = re.search(r"addEventListener\('webglcontextrestored', \(\) => \{(.*?)\n    \}\);", self.js, re.S)
        self.assertTrue(restored)
        for step in ("compileThemes(canvas)", "root.classList.replace('no-gl', 'has-gl')", "wake()"):
            self.assertIn(step, restored[1])

    def test_the_scanner_bar_keeps_to_the_middle_column_and_off_the_tagline(self):
        """Beside the claims the bar is at most the middle column plus part of the gap either
        side, so it never runs under them. It is sized from the word's ink, not the h1's box,
        which is as wide as the column, and its foot stays above the tagline."""
        three = [body for selectors, body in self.rules if selectors == ".hero-in"
                 and "grid-template-columns: minmax(0, 1fr) minmax(" in body]
        self.assertEqual(len(three), 1)
        column_gap = int(re.search(r"gap: \d+px (\d+)px", three[0])[1])
        spare = int(re.search(r"below \? 640 : copyWidth \+ (\d+)", self.js)[1])
        self.assertLessEqual(spare, 2 * column_gap - 8, "a little air between the bar and the claims")
        measure = re.search(r"function measure\(\) \{(.*?)\n    \}", self.js, re.S)[1]
        self.assertIn("selectNodeContents(title)", measure)
        self.assertIn("inkWidth: word.getBoundingClientRect().width", measure)
        self.assertIn("taglineTop: tagline.getBoundingClientRect().top - area.top", measure)
        self.assertIn("roof: Math.max(0, header.offsetHeight - area.top - scrollY)", measure)
        self.assertIn("Math.max(inkWidth * 2.3, 320)", self.js)
        self.assertIn("Math.min(width - 32, ", self.js, "on a phone, inside the 16 px gutter")
        self.assertIn("Math.max(foot - roof - 4, titleHeight + 12) / theme.aspect", self.js,
                      "its top below the header")
        self.assertIn("const foot = taglineTop - 4;", self.js)
        self.assertRegex(self.js, r"y: Math\.min\(titleTop \+ titleHeight / 2 - h / 2, foot - h\)")
        # The one theme with black behind the name: there the name is light, in either mode. With
        # the shader the script turns it with the bar's fade; the still drawing goes by the theme.
        self.assertIn("color: #f1f2f7", self.rule(".has-gl .hero.ink-light h1"))
        self.assertIn("color: #f1f2f7", self.rule(':root:not(.has-gl) .hero[data-theme="scanner"] h1'))
        self.assertIn("hero.classList.toggle('ink-light', isBar(theme) && fade > .5);", self.js)

    def test_on_a_wide_screen_the_spare_height_goes_above_the_name(self):
        """Where the claims sit beside the words, the hero's minimum height leaves room over the
        name, where a figure peeks out, and none under the picker: the ticker follows close."""
        three = [body for selectors, body in self.rules if selectors == ".hero-in"
                 and "grid-template-columns: minmax(0, 1fr) minmax(" in body]
        self.assertEqual(len(three), 1)
        self.assertIn("min-height: var(--hero-h)", three[0])
        self.assertIn("align-content: end", three[0])
        self.assertLessEqual(int(re.search(r"--hero-h: min\((\d+)px", self.rule(":root"))[1]), 300)
        self.assertIn("padding-block: 16px 14px", self.rule(".hero"))
        self.assertIn("margin-block-start: 16px", self.rule(".ticker"))

    def test_the_header_blurs_a_layer_inside_it_not_itself(self):
        for page in self.pages:
            self.assertRegex(page.read_text(), r'<header class="site-header" id="site-header">\s*'
                                               r'<div class="nav-glass" aria-hidden="true"></div>', page)
        for selectors, body in self.rules:
            for selector in selectors.split(","):
                subject = re.split(r"[\s>+~]+", selector.strip())[-1]
                if re.match(r"(\.site-header|#site-header|header)(?![\w-])", subject):
                    self.assertNotIn("backdrop-filter", body, selector)
        header = self.rule(".site-header")
        for declaration in ("position: fixed", "inset: 0 0 auto", "z-index: 100", "isolation: isolate",
                            "will-change: transform"):
            self.assertIn(declaration, header)
        self.assertIn("transform: translateY(-100%)", self.rule(".site-header.is-hidden"))
        glass = self.rule(".nav-glass")
        for declaration in ("position: absolute", "inset: 0", "z-index: -1", "pointer-events: none",
                            "background: var(--bg)"):    # solid where the browser can't blur
            self.assertIn(declaration, glass)
        self.assertRegex(self.css, r"@supports \(\(backdrop-filter: blur\(1px\)\) or \(-webkit-backdrop-filter: "
                                   r"blur\(1px\)\)\) \{\s*\.nav-glass \{[^}]*-webkit-backdrop-filter: blur\(20px\) "
                                   r"saturate\(145%\);\s*backdrop-filter: blur\(20px\) saturate\(145%\)")
        self.assertRegex(self.css, r"@media \(prefers-reduced-transparency: reduce\) \{\s*\.nav-glass \{"
                                   r"[^}]*backdrop-filter: none")
        self.assertIn("padding-top: var(--header-h)", self.rule("body"))
        self.assertIn("scroll-padding-top", self.rule("html"))
        reduced = self.css[self.css.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertRegex(reduced, r"\.site-header \{ transition: none")

    def test_the_header_hides_on_the_way_down_and_comes_back_on_the_way_up(self):
        self.assertRegex(self.js, r"const delta = y - lastY;\s*lastY = y;")
        self.assertRegex(self.js, r"if \(y < 96 \|\| performance\.now\(\) < graceUntil\) showHeader\(\);\s*"
                                  r"else if \(delta > 6\) header\.classList\.add\('is-hidden'\);\s*"
                                  r"else if \(delta < -6\) showHeader\(\);")
        self.assertIn("const showHeader = () => header.classList.remove('is-hidden');", self.js)
        self.assertIn("graceUntil = performance.now() + 900", self.js)
        self.assertIn("}, { passive: true });", self.js)
        # A grace after every jump: the first load, a link to a section, back and forward.
        for event in ("hashchange", "popstate", "pageshow"):
            self.assertIn(f"addEventListener('{event}', navigated)", self.js)
        self.assertRegex(self.js, r"closest\?\.\('a\[href\^=\"#\"\]'\)\) navigated\(\)")
        self.assertIn("header.addEventListener('focusin', showHeader)", self.js)
        # The jump's own scroll comes with the next frame, which a busy phone can hold back past
        # the grace: the first scroll after a jump restarts it, so a late one doesn't hide the header.
        self.assertRegex(self.js, r"const navigated = \(\) => \{\s*graceUntil = performance\.now\(\) \+ 900;\s*"
                                  r"jumpUntil = performance\.now\(\) \+ 3000;\s*showHeader\(\);\s*\};")
        self.assertRegex(self.js, r"lastY = y;\s*if \(performance\.now\(\) < jumpUntil\) \{\s*jumpUntil = 0;\s*"
                                  r"graceUntil = Math\.max\(graceUntil, performance\.now\(\) \+ 900\);\s*\}\s*"
                                  r"if \(y < 96 \|\| performance\.now\(\) < graceUntil\) showHeader\(\);")

    def test_the_shader_spares_a_phone(self):
        for rule in ("Math.min(devicePixelRatio || 1, 1.5", "IntersectionObserver", "'visibilitychange'",
                     "document.hidden", "prefers-reduced-motion: reduce"):
            self.assertIn(rule, self.js)
        # Only while some of what it draws is on screen, not just some of the hero.
        self.assertIn("seenFrom < spot.y + spot.height", self.js)
        self.assertIn("seenFrom = entry.intersectionRect.top - entry.boundingClientRect.top", self.js)

    def test_the_motion_can_be_paused_and_holds_still_on_the_picker(self):
        """WCAG 2.2.2: the moving backdrop behind the words has a pause button, and the turns
        hold while the pointer or the keyboard is on the picker, so its label doesn't change
        under the visitor."""
        for page in self.pages:
            code = "en" if page.parent == self.build.DIST else page.parent.name
            label = json.loads((ROOT / "website/src/i18n" / f"{code}.json").read_text())["pauseMotion"]
            self.assertTrue(label.strip(), page)
            data = json.loads(re.search(r'<script type="application/json" id="strings">(.*?)</script>',
                                        page.read_text(), re.S)[1])
            self.assertEqual(data["pauseMotion"], label, page)
            play = json.loads((ROOT / "website/src/i18n" / f"{code}.json").read_text())["playMotion"]
            self.assertTrue(play.strip(), page)
            self.assertEqual(data["playMotion"], play, page)
        self.assertIn("pause.setAttribute('aria-label', S.pauseMotion)", self.js)
        self.assertIn("pause.title = paused ? S.playMotion : S.pauseMotion;", self.js)
        self.assertIn("pick.append(pause);", self.js)
        self.assertRegex(self.js, r"store\.set\('still', paused \? '1' : '0'\);\s*if \(paused\) redraw\(\);")
        # Only the keyboard holds by focus: a click or a tap also focuses a button in Chromium.
        self.assertIn("pick.addEventListener('focusin', event => { focused = byKeyboard(event.target); hold(); });",
                      self.js)
        self.assertIn("element.matches(':focus-visible')", self.js)
        self.assertIn("pause.setAttribute('aria-pressed', paused)", self.js)
        self.assertIn("const still = () => reducedMotion || paused;", self.js)
        self.assertRegex(self.js, r"function loop\(time\) \{[^}]*if \(lost \|\| still\(\) \|\| !onScreen\(\)\) return;")
        for event in ("pointerenter", "pointerleave", "focusin", "focusout"):
            self.assertIn(f"pick.addEventListener('{event}'", self.js)
        self.assertIn("if (!still() && !manual && turn > SLIDE_S)", self.js)
        self.assertIn("const turn = age - heldFor - (heldAt ? now - heldAt : 0);", self.js)
        self.assertIn("background: currentColor", self.rule('.still[aria-pressed="true"]::before'))

    def test_a_still_frame_shows_tux_listening_with_his_eyes_open(self):
        """Reduced motion and pause draw one fixed moment, not the live clock, which could land
        in a blink. The moment is a listening one, as the label says."""
        match = re.search(r"const t = still\(\) \? ([\d.]+) : now;", self.js)
        self.assertTrue(match)
        moment = float(match[1])
        self.assertIn("gl.uniform1f(u.u_time, t % 1000)", self.js)
        self.assertIn("Math.abs(Math.sin(t * 5.1) * Math.sin(t * 1.7 + 1))", self.js, "the level, from it too")
        shader = (ROOT / "themes/tux/shader.glsl").read_text()
        blinks = re.findall(r"smoothstep\(([\d.]+), ([\d.]+), abs\(mod\(u_time, ([\d.]+)\) - ([\d.]+)\)\)", shader)
        self.assertEqual(len(blinks), 2, "the two blink clocks")
        for _, shut_until, period, at in blinks:
            self.assertGreater(abs(moment % float(period) - float(at)), float(shut_until), (period, at))
        age = float(re.search(r"const age = still\(\) \? ([\d.]+) : now - slideStart;", self.js)[1])
        self.assertTrue(0.6 < age < 2.6, "inside the listening part of a turn, past its fade")

    def test_without_script_the_hero_offers_the_linux_download_and_names_it(self):
        """As a section's download does: the page arrives with the Linux file, named, and the version
        under it, and app.js then fits all three to the visitor's own system."""
        releases = json.loads((ROOT / "website/src/releases.json").read_text())
        linux = releases["linux"]
        for page in self.pages:
            code = "en" if page.parent == self.build.DIST else page.parent.name
            strings = json.loads((ROOT / "website/src/i18n" / f"{code}.json").read_text())
            _, hero = self.hero(page)
            label = html_escape(self.build.fill(strings["dlFor"], os="Linux"))
            self.assertIn(f'<a class="btn btn-main" id="download" href="{html_escape(linux["url"])}">{label}</a>',
                          hero, page)
            meta = self.build.fill(strings["dlMeta"], version=linux["version"], channel=strings[linux["channel"]])
            self.assertIn(f'<span id="dl-meta-text">{html_escape(meta)}</span> · ', hero, page)
        # With no Linux release yet, it says what is coming and downloads nothing.
        releases["linux"]["url"] = None
        self.assertEqual(self.build.hero_download(strings, releases),
                         '<a class="btn btn-main" id="download" aria-disabled="true">'
                         f'{html_escape(self.build.fill(strings["dlPending"], os="Linux"))}</a>')
        for line in ("button.textContent = fill(S.dlFor, { os: osName });", "button.href = release.url;",
                     "button.removeAttribute('aria-disabled');", "meta.textContent = release.version"):
            self.assertIn(line, self.js)

    def test_the_tagline_breaks_evenly_and_between_its_sentences(self):
        """Over the theme, right under the title: two even lines on a phone, not one word alone."""
        for page in self.pages:
            code = "en" if page.parent == self.build.DIST else page.parent.name
            text = json.loads((ROOT / "website/src/i18n" / f"{code}.json").read_text())["tagline"]
            tagline = re.search(r'<p class="tagline">(.*?)</p>', page.read_text())[1]
            self.assertEqual(len(re.findall(r"<span>[^<]+</span>", tagline)), 2, page)
            self.assertEqual(re.sub(r"</?span>", "", tagline), html_escape(text), page)
        self.assertIn("text-wrap: balance", self.rule(".tagline"))
        span = self.rule(".tagline span")
        self.assertIn("display: inline-block", span)
        self.assertIn("text-wrap: balance", span)

    def test_the_words_over_a_theme_keep_their_contrast_without_a_blur(self):
        """The claims sit over a band on a wide screen, so they are nearly solid: 4.5:1 for Now and
        Soon over a ribbon. The pills under the button blur nothing, since the canvas behind them
        redraws every frame; and asking for less transparency makes them all solid."""
        self.assertIn("background: color-mix(in srgb, var(--panel) 94%, transparent)", self.rule(".hero .cap"))
        for selector in (".hero .cap", ".hero .dl-meta", ".hero .dl-preview", ".hero .pick"):
            self.assertNotIn("backdrop-filter", self.rule(selector), selector)
        self.assertIn("background: color-mix(in srgb, var(--bg) 90%, transparent)", self.rule(".hero .pick"))
        reduced = re.findall(r"@media \(prefers-reduced-transparency: reduce\) \{((?:[^{}]*\{[^{}]*\})*)\s*\}", self.css)
        reduced = " ".join(reduced)
        self.assertRegex(reduced, r"\.hero \.dl-meta, \.hero \.dl-preview, \.hero \.pick \{ background: var\(--bg\); \}")
        self.assertRegex(reduced, r"\.hero \.cap \{ background: var\(--panel\); \}")

    def test_the_picker_is_a_thumb_wide_on_a_touch_screen(self):
        self.assertRegex(self.css, r"@media \(pointer: coarse\) \{\s*\.dot, \.still \{ width: 34px; height: 34px; \}")
        self.assertIn("width: 9px; height: 9px", self.rule(".dot::before"), "the marks stay the same size")

    def test_the_theme_shown_is_marked_by_shape_not_only_colour(self):
        self.assertIn("background: var(--text)", self.rule('.dot[aria-pressed="true"]::before'))
        self.assertIn("width: 18px", self.rule('.dot[aria-pressed="true"]::before'))
        self.assertIn("color: var(--muted)", self.rule(".dot"))     # 3:1 and more on the page



@unittest.skipUnless(shutil.which("node"), "needs node")
class HeroScript(unittest.TestCase):
    """app.js itself, run by node on a fake page (tests/website_hero_page.mjs) whose clock and
    animation frames move only when a step says so, as a visitor would use the hero."""

    @classmethod
    def setUpClass(cls):
        build = load_build()
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        build.DIST = Path(folder.name) / "dist"
        with contextlib.redirect_stdout(io.StringIO()):
            build.main()
        cls.dist = build.DIST
        js = (build.DIST / "app.js").read_text()
        cls.slide = float(re.search(r"const SLIDE_S = ([\d.]+);", js)[1])
        cls.fade = float(re.search(r"const FADE_S = ([\d.]+);", js)[1])
        cls.moment = float(re.search(r"const t = still\(\) \? ([\d.]+) : now;", js)[1])
        cls.strings = json.loads((ROOT / "website/src/i18n/en.json").read_text())

    def play(self, *steps):
        result = subprocess.run(["node", str(ROOT / "tests/website_hero_page.mjs"), str(self.dist), json.dumps(steps)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])     # the last line: a node shim may add one

    def test_a_jump_keeps_the_header_even_when_its_scroll_comes_late(self):
        """The scroll a link makes is reported with the next frame, which a busy phone can hold back
        past the grace. It still counts as the jump; scrolling down after it hides the header."""
        seen = self.play({"wait": 5}, {"follow": "#versions"}, {"wait": 1.5}, {"scroll": 1400},
                         {"read": "landed"}, {"wait": 1.5}, {"scroll": 1460}, {"read": "down"},
                         {"scroll": 1420}, {"read": "up"})
        self.assertFalse(seen["landed"]["headerHidden"], "a late jump is still the jump")
        self.assertTrue(seen["down"]["headerHidden"], "the visitor's own scroll down hides it")
        self.assertFalse(seen["up"]["headerHidden"], "and a scroll up brings it back")
        # The first scroll counts only once, and only soon after a jump: a scroll down long after
        # the page arrived, with no link followed, hides the header.
        seen = self.play({"wait": 5}, {"scroll": 1400}, {"read": "down"})
        self.assertTrue(seen["down"]["headerHidden"])
        seen = self.play({"wait": 5}, {"follow": "#versions"}, {"scroll": 1400}, {"wait": 1.5},
                         {"scroll": 1460}, {"read": "down"})
        self.assertTrue(seen["down"]["headerHidden"], "a jump's grace doesn't outlast it")

    def test_play_after_a_pause_by_pointer_brings_the_turns_back(self):
        """Chromium focuses a button that is clicked or tapped, and the focus stays after the
        pointer has gone. That isn't the keyboard on the picker, so it must hold nothing."""
        seen = self.play({"enter": True}, {"press": "still", "by": "pointer"}, {"wait": 1},
                         {"press": "still", "by": "pointer"}, {"leave": True},
                         {"wait": self.slide + 1}, {"read": "later"})["later"]
        self.assertEqual(seen["focused"], "still", "the play button keeps focus")
        self.assertFalse(seen["focusVisible"])
        self.assertEqual(seen["pause"]["pressed"], "false")
        self.assertNotEqual(seen["theme"], "tux", "the next theme has had its turn")

    def test_the_keyboard_on_the_picker_holds_the_theme_until_it_leaves(self):
        seen = self.play({"wait": .5}, {"focus": "dot:0"}, {"wait": 2 * self.slide},
                         {"press": "still", "by": "keyboard"}, {"press": "still", "by": "keyboard"},
                         {"wait": 2 * self.slide}, {"read": "held"},
                         {"blur": True}, {"wait": self.slide + 1}, {"read": "left"})
        self.assertTrue(seen["held"]["focusVisible"])
        self.assertEqual(seen["held"]["theme"], "tux")
        self.assertEqual(seen["left"]["theme"], "orb")

    def test_pause_draws_one_still_frame_and_play_moves_on_from_it(self):
        seen = self.play({"wait": 2}, {"read": "moving"}, {"press": "still", "by": "pointer"}, {"wait": .2},
                         {"read": "paused"}, {"wait": 2 * self.slide}, {"read": "later"},
                         {"press": "still", "by": "pointer"}, {"wait": 1}, {"read": "playing"})
        self.assertNotEqual(seen["moving"]["frame"]["u_time"], self.moment)
        paused = seen["paused"]
        self.assertEqual(paused["frame"]["u_time"], self.moment, "drawn at once, at the still moment")
        self.assertEqual(paused["frame"]["u_appear"], 1)
        self.assertEqual(paused["pause"], {"pressed": "true", "title": self.strings["playMotion"],
                                           "label": self.strings["pauseMotion"]})
        self.assertEqual(seen["later"]["frames"], paused["frames"], "nothing more is drawn while paused")
        self.assertEqual(seen["later"]["theme"], "tux", "and the turns stop")
        playing = seen["playing"]
        self.assertGreater(playing["frames"], paused["frames"])
        self.assertEqual(playing["pause"]["title"], self.strings["pauseMotion"])
        self.assertEqual(playing["theme"], "tux")

    def test_the_name_turns_light_and_back_with_the_bar(self):
        """It turns as the bar fades in and out, not a moment after the next theme has begun;
        a band, as wide as the bar is, never turns it."""
        seen = self.play({"until": "scanner"}, {"read": "arriving"}, {"wait": 1}, {"read": "shown"},
                         {"wait": self.slide - 1 - self.fade * .4}, {"read": "going"},
                         {"until": "tux"}, {"read": "next"}, {"until": "waves"}, {"wait": 1}, {"read": "band"})
        self.assertTrue(seen["shown"]["gl"])
        self.assertFalse(seen["arriving"]["inkLight"])
        self.assertTrue(seen["shown"]["inkLight"])
        self.assertEqual(seen["going"]["theme"], "scanner")
        self.assertFalse(seen["going"]["inkLight"])
        self.assertFalse(seen["next"]["inkLight"])
        self.assertFalse(seen["band"]["inkLight"])


if __name__ == "__main__":
    unittest.main()
